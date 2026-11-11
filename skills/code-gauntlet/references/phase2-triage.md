# Phase 2 Triage Reference

Sub-steps, detection logic, and **args preparation** for Phase 2: Target, Triage & Args Preparation.

## Contents

- **2a** VCS platform detection — **2b** Working tree checkout — **2b-post** SHA + previously-reviewed gate + stale cleanup — **2c** Review target + diff/changed-files save
- **2d** Project context (CLAUDE.md, REVIEW.md collection) — **2e** Risk classification
- **2f** AI-generated code detection — **2g** Review dimensions
- **Shared agent context file** — **Args preparation** (the args waist the workflow consumes)
- **Triage announcement**

---

## 2a. Detect VCS Platform

Auto-detect from `git remote get-url origin`:

- GitHub → `gh` CLI, "PR"
- GitLab (including self-hosted) → `glab` CLI, "MR"

If detection fails, ask the user.

---

## 2b. Ensure Working Tree Reflects Review Target

Before running any diff commands, confirm the local working tree matches the review target. Use the `pr_number` resolved in Phase 1 — never extract PR numbers from branch names (branch names may contain upstream PR numbers that differ from the PR number in the current repo).

**[phase2-preparation.md: Phase 2 Composite A](phase2-preparation.md#phase-2-composite-a--pre-gather-status--checkout--sha--prior-review-gate--stale-truncation) owns the literal blocks for GitHub, GitLab, and branch targets.** Run those blocks as written; this section explains their target resolution and failure rules.

**1. Resolve the target's head SHA:**

**PR/MR mode (`pr_number` set):** run the single capture/status recipe in Composite A, linked above. It validates the head and canonical target and captures the body as unverified claims for framed shared-context delivery. Reuse its status receipt.

- **Branch comparison:** use the target-branch heredoc in [Composite A](phase2-preparation.md#phase-2-composite-a--pre-gather-status--checkout--sha--prior-review-gate--stale-truncation).

Resolve the comparison base (the repository's default branch or the user's chosen base) with the literal `CODE_GAUNTLET~BASE` block in [phase2-preparation.md: Phase 2 Composite A](phase2-preparation.md#phase-2-composite-a--pre-gather-status--checkout--sha--prior-review-gate--stale-truncation), in its own Bash call. The resolver looks the name up as a local branch, then a remote-tracking branch (`origin/main`), and refuses a name that is both; a name that is neither, such as a tag, a SHA or `HEAD~1`, resolves as a git revision. If either call exits non-zero, stop and report its one-line error. Otherwise record the printed SHAs as `{target_sha}` and `{base_sha}` and type those hex values into later commands; shell variables do not survive between Bash calls. Never put the heredoc inside `$( )`: bash 3.2 parses its body for parentheses. `~` cannot appear in a git ref name, so no branch name can end the heredoc early.

- **Local changes:** HEAD — no-op, already on correct state

**2. Compare against current HEAD:**

```
git rev-parse HEAD
```

If the SHA matches → proceed to 2c.

**3. If mismatch → checkout:**

| Target type | Command |
|---|---|
| PR/MR number or URL | `gh pr checkout <number>` (GitHub) / `glab mr checkout <number>` (GitLab) |
| Branch name | step 1's target heredoc with `resolve_branch.py --checkout` |
| Local changes | no-op |

> Headless exception (`configResult.mode == "headless"`): never run any checkout command from
> this table — the harness pre-places the working tree at the pinned head. If the
> step-2 SHA comparison mismatches, print `HEADLESS INPUT ERROR: working tree HEAD
> <sha> != PR head <sha>` and stop with a non-zero outcome; never silently review
> different code. See `references/headless-mode.md`.

**4. If checkout fails → STOP immediately:**

```
Unable to checkout [branch/PR]. The review requires the target code to be accessible locally.
You can checkout the branch manually and re-run the review.
```

No fallback or workaround — a silently wrong working tree produces unreliable review results.

---

## 2b-post. Resolve Head SHA, Previously-Reviewed Gate, and Clean Stale Files

Now that the working tree reflects the review target, compute the short SHA and perform housekeeping. **Run [phase2-preparation.md: Phase 2 Composite A](phase2-preparation.md#phase-2-composite-a--pre-gather-status--checkout--sha--prior-review-gate--stale-truncation) as one Bash call whose sections form the genuine dependency chain below.** That section owns the full runnable script; this section explains the reasoning behind its ordering.

**1. Resolve head SHA** (`sha` section) — after checkout, so it reflects the actual PR HEAD, not whatever branch was checked out before.

**2. Output-dir ignore** — owned by `scripts/ensure_output_dir.py`, invoked in Phase 1. Phase 2 does not re-establish ignore and has no `gitignore` composite section. Never append to the repo's tracked `.gitignore`.

**3. Previously-reviewed gate** (`pr_url` + `prior_review` sections; PR/MR targets only — skip for `local`):

Runs the gate documented in `phase1-preflight.md` → "Previously-Reviewed Gate", which resolves whether this PR/MR was already reviewed and whether the incremental path is safe. Use Composite A's `pr_url` section so the detector uses the PR's base repository and host.

A Skip answer below stops the review without reverting the checkout.

If the gate resolves to **Incremental**, store `last_reviewed_sha` — section 2c's incremental branch consumes it. If it resolves to **Skip**, stop the run here. Otherwise continue as a full review.

**4. Truncate stale files** with `scripts/stale_truncate.py` so a re-run does not blend old artifacts with new; handle `DEFERRED` as in Composite A.

---

## 2c. Identify Review Target

Use `target_type` and `pr_number` from Phase 1's "Resolve review target" step. Do not re-derive the PR number here.

1. **PR/MR mode** (`pr_number` set) — Use Composite A's status receipt and Composite B's saved diff commands. Get full SHA: `git rev-parse HEAD`
   - **GitHub (PR):** Gather the file list with `{python} "{plugin_root}/scripts/list_pr_files.py" --platform github --pr {pr_number}`. Gather the full diff with `gh pr diff {pr_number}`.
   - **GitLab (MR):** Gather the file list with `{python} "{plugin_root}/scripts/list_pr_files.py" --platform gitlab --pr {pr_number}` (the `files` producer in [phase2-preparation.md: Phase 2 Composite B](phase2-preparation.md#phase-2-composite-b--gather-diff-changed-files-line-count-misc)). Gather the full diff with `glab mr diff {pr_number}`.
2. **Branch comparison** — use 2b's resolved SHAs in [Composite B's branch comparison blocks](phase2-preparation.md#phase-2-composite-b--gather-diff-changed-files-line-count-misc).
3. **Local changes** — use [Composite B's local changes blocks](phase2-preparation.md#phase-2-composite-b--gather-diff-changed-files-line-count-misc).
4. **Incremental** (2b-post step 3's gate resolved "Incremental"; PR/MR mode only) — use [Composite B's incremental path](phase2-preparation.md#phase-2-composite-b--gather-diff-changed-files-line-count-misc).

   An unbounded diff would include unrelated files from a base-branch merge. A base merge that touches a PR file still appears. Validate the saved patch as below. If the command fails or the patch is empty, save branch 1's full server diff instead and disclose the fallback. Record `last_reviewed_sha` in the triage announcement and Phase 8 methodology.

**Save the diff and the changed-file list (the workflow has no git access):** Persist both git-derived inputs to disk so the workflow can consume them.

1. **Changed files** → `{output_dir}/code-gauntlet-files-{head_sha_short}.json` as a JSON array (this path becomes `args.changedFilesPath`). Keep the same array inline for `args.changedFiles` — the Summarize stage reads it by value, because the workflow cannot open the file. On the incremental path, this stays branch 1's full server file list unchanged — the same list branch 4 uses to bound the diff, never a narrower incremental-only set. Saved **first**, so branch 4's incremental diff (below) has a file list already on disk to bound against.
2. **Diff** → `{output_dir}/code-gauntlet-diff-{head_sha_short}.patch`. In PR/MR mode use the server-computed, fork-safe diff (or, when 2b-post step 3 resolved incremental, branch 4's `incremental_diff.py` command); for branch/local targets use `save_git_diff.py` from steps 2/3, which refuses empty or malformed patches. This path becomes `args.diffPath` and is passed to the verify executor as `--diff-file`.

**Run these saves as [phase2-preparation.md: Phase 2 Composite B](phase2-preparation.md#phase-2-composite-b--gather-diff-changed-files-line-count-misc)** — one Bash call. `files` runs first and gates the rest: the incremental diff reads its list, and `numstat` reads the saved patch. That section owns the exact script.

`changedLines` (threaded into `args.changedLines`, Args Preparation below) is counted from the diff actually saved above — the incremental diff on the incremental path, never branch 1's full-PR diff.

For PR/MR server diffs, validate the saved diff before relying on it. Branch/local validation is enforced by `save_git_diff.py`:

- Non-empty (file size > 0)
- Starts with `diff --git` (confirms it is a valid unified diff, not an error message)

---

## 2d. Gather Project Context

After 2c saves the changed-file list, follow [phase2-preparation.md: Collect project rules](phase2-preparation.md#collect-project-rules).

### REVIEW.md Detection

Complete 2d before 2e.

A successful empty discovery never blocks or asks a question. Collector failures stop the handoff.

---

## 2e. Classify Changed Files by Risk Level

- **High risk** — auth, security, payment, data access, public APIs, DB migrations, crypto, infra/deploy, permission/RBAC. Also >200 lines changed. Also: files implementing a cache, proxy, decorator, or delegation pattern (indirection can obscure contracts and state ownership — flag these even if the diff appears mechanical).
- **Medium risk** — business logic, services, controllers, middleware, state management. 50-200 lines changed.
- **Low risk** — tests, docs, config, generated code, lockfiles, formatting-only. <50 lines changed.

**Content-change promotion.** After initial classification, check LOW-risk files for substantive content changes — any diff line that changes a string value, numeric value, or identifier (not just formatting, whitespace, markup, or delimiters). Files with substantive content changes get promoted to MEDIUM. This is type-agnostic.

Promotion triggers: i18n text changes, config value changes, CSS/SCSS numeric changes, changed string literals or identifiers.
Stay LOW: lock files, whitespace-only changes, generated code updates, tag case changes (`<br/>` → `<br />`).

### Light Review for Trivial PRs

If ALL files are low-risk AND total lines <50, ask Light review vs Full review (template in `references/phase1-preflight.md`). Stamp the answer verbatim into `args.scopeAnswer` (`"light"` or `"full"`); the workflow derives the dimensions from it (2g). Announce the actual dimension set — `bugs, security` for light, the full list for full.

> Headless exception: do not ask, and leave `scopeAnswer` out; interactive runs stamp the question answer.

Only stamp `scopeAnswer` when this gate actually fired (every file low-risk AND total lines <50); omit it entirely otherwise — the args waist refuses a `scopeAnswer` the riskTable/changedLines don't support, and refuses a light-eligible riskTable with no `scopeAnswer` at all (the gate must have asked).

---

## 2f. AI-Generated Code Detection

Scan for AI co-author trailers, attribution comments, AI tool metadata. **Elevate AI-generated files one risk level**. Include AI-generation status in risk classification sent to all agents.

**Tool instructions:**

Use **Grep** to search for AI co-author indicators in changed files:

- Git trailers: `Co-Authored-By`, `Co-authored-by`, `Copilot-By`
- Comments: patterns like `AI-generated`, `generated by`, `GPT`, `Claude`, `Copilot`, `ChatGPT`
- Metadata: language-specific markers (e.g., `<!-- AI generated -->`, `# AI generated`)

Example:

```
Grep(pattern: "Co-[Aa]uthored-[Bb]y|Copilot-By", path: "<each explicit changed-file path>")
Grep(pattern: "AI-generated|generated by (GPT|Claude|Copilot|ChatGPT)", path: "<each explicit changed-file path>")
Grep(pattern: "<!-- AI|# AI generated|// AI generated", path: "<each explicit changed-file path>")
```

Also run the same patterns directly against the saved diff — `Grep(pattern: "...", path: "{output_dir}/code-gauntlet-diff-{head_sha_short}.patch")` — to catch markers in added lines the working-tree scan above might miss. This patch is already on disk from Composite B's `diff` section (2c); never invoke a fresh `gh pr diff` here to re-fetch it.

Never use `grep` or `find` from Bash for AI detection.

---

## 2g. Determine Review Dimensions

The workflow computes the dispatched dimensions with `deriveAgentFlags(riskTable, changedLines, scopeAnswer)` in `workflows/src/discover.js`.
A `light` answer on a light-eligible diff (every changed file low-risk and fewer than 50 changed lines) yields `{ deep: false }` and dispatches only bug-detector and security-reviewer.
Every other valid run dispatches all seven specialists, followed by a gap hunter when citation-only coverage leaves reportable regions within its read budget. Light scope disables the hunter.

---

## Shared Agent Context File

The workflow's **summarize, discovery, and validate** agents Read `{output_dir}/code-gauntlet-context-{head_sha_short}.md`; write it before the Phase 3 `Workflow` call.

Use the command in [phase2-preparation.md: Write the shared agent context file](phase2-preparation.md#write-the-shared-agent-context-file) and `scripts/write_shared_context.py`; pass the 2e risk classification and 2f AI-generated-code status as Markdown on stdin.

### Measure it

Stamp the script receipt verbatim. Size fields are optional only so a skipped step degrades to a disclosed `context_unmeasured` gap, never a licence to skip.

---

## Args Preparation

Assemble the args waist the workflow consumes. It is a single JSON object passed as the `Workflow` tool's `args` parameter (Phase 3) — not written to disk. The workflow validates it up front (`validateArgs`) and rejects a malformed waist before any dispatch.

<!-- generated-from-registry-identity:pr_identity_fields — do not edit; run scripts/generate_contract_requirements.py -->
`delivery.prIdentity` fields:

- `owner` (required): a non-empty string.
- `repo` (required): a non-empty string.
- `pr_number` (required): a positive safe integer.
- `sha_full` (required): a 40-character lowercase hex commit id.
- `platform` (required): one of github, gitlab.
- `web_origin` (required): an http(s) URL without userinfo (normalized to its origin).
- `title` (optional): a non-empty string when present.

Producer commands:

`gh pr view {pr_number} --json url,title | {python} "{plugin_root}/scripts/resolve_pr_identity.py" --platform github --sha "$(git rev-parse HEAD)"`

`glab mr view {pr_number} --output json | {python} "{plugin_root}/scripts/resolve_pr_identity.py" --platform gitlab --sha "$(git rev-parse HEAD)"`

The title and URL reach the script only through the pipe.
<!-- /generated-from-registry-identity:pr_identity_fields -->

**Omit optional fields you have no value for; never stamp an explicit `null`.**
The waist treats `null` as absent for `delivery` and `checkpoints`.

**Required fields (`validateArgs` fails loud without them):**

| Field | Value |
|---|---|
| `argsVersion` | `1` |
| `mode` | Copy `configResult.mode` from the resolver JSON |
| `repoRoot` | `configResult.identity.repo_root` (Python-resolved wire path) |
| `outputDir` | resolved `{output_dir}` (absolute) |
| `headShaShort` | `head_sha_short` from 2b-post |
| `nonce` | the run nonce from Composite A, preserved on resume, matching `^[A-Za-z0-9._-]+$` (interpolated into the verify executor argv per slice — no whitespace/shell metacharacters) |
| `generatedAt` | current wall-clock as an ISO8601 string — the workflow's injected clock (it never calls `new Date()`) |
| `diffPath` | `{output_dir}/code-gauntlet-diff-{head_sha_short}.patch` |
| `changedFiles` | the changed-file array, by value (Summarize bucketing; the workflow has no disk access) |
| `changedLines` | total changed line count, by value (Summarize bucketing threshold) |
| `riskTable` | the Phase 2e per-file risk classification, by value, as `[{ path, risk }]` — `path` set must equal `changedFiles` exactly (missing or extra paths fail loud; see Phase 2e's risk-level table for the `risk` contract) |
| `configEcho` | Copy `configResult.waist` verbatim into args, including all fields it carries. |
| `pluginRoot` | `configResult.identity.plugin_root`; the workflow derives its script paths from this root. |
| `python` | Phase 1 selection: `python3`, `python`, or `py -3`; optional, defaults to `python3`. |
| `reviewScope` | Stamp per [phase2-preparation.md: Composite A](phase2-preparation.md#phase-2-composite-a--pre-gather-status--checkout--sha--prior-review-gate--stale-truncation), after the prior-review gate. |
| `policy` | `{ tier, subagentModel, provider, gateway, mappedAliases }` — see below |
| `limits` | Stamp `{}`. |
| `delivery` | For PR/MR targets, stamp only `{ prIdentity }` using the generated field contract above. Omit it for local targets. |

<!-- generated-from-registry-identity:derived_waist_fields — do not edit; run scripts/generate_contract_requirements.py -->
The workflow derives these waist fields from the copied `configEcho` receipt. Do not stamp a derived field; when a listed derivation applies, a stamped value that disagrees with its receipt is refused before dispatch.

- `limits.deliveryCap` (headless and interactive runs): from `configEcho.pr_comment_cap`; digits derive as a JSON number; on interactive runs the receipt spelling `null` derives as JSON `null`. Stamp `limits` and leave `deliveryCap` out of it.
- `delivery.tier` (headless and interactive runs): from `configEcho.delivery_tier`. Leave `tier` out of any stamped `delivery`.
- `reviewScope.requested` (headless runs, when `reviewScope.detector` is an object): from `configEcho.reviewed_policy`; `skip` derives as `full`. Stamp `reviewScope` and leave `requested` out of it.
- `scopeAnswer` (headless runs, when every changed file is low risk and fewer than 50 lines changed): from `configEcho.trivial_scope`. Leave it out.
<!-- /generated-from-registry-identity:derived_waist_fields -->

**`policy` (model policy the workflow runs under):**

Run `scripts/capture_policy_env.py` and stamp its four keys verbatim. The workflow cannot read `process.env`.

- `tier` - copy `configResult.resolved.model_tier`; the resolver owns its value and source.
- `subagentModel` - `CLAUDE_CODE_SUBAGENT_MODEL`, or `null` when unset or blank. If non-null, warn the user and record the override in the methodology.
- `provider` - first truthy switch in Bedrock, Vertex, Foundry order (`CLAUDE_CODE_USE_<PROVIDER>`), else `firstParty`. Truthy means `1`/`true`/`yes`/`on`, trimmed and case-insensitive. Other provider spellings are rejected at the waist.
- `gateway` - stamp `policy.gateway` as `true` iff `ANTHROPIC_BASE_URL` is nonblank after trimming. It leaves `provider` unchanged and disables conditional per-dimension schemas because gateways can forward schemas to another backend. Absent or null equals `false`.
- `mappedAliases` - distinct model aliases whose `ANTHROPIC_DEFAULT_<ALIAS>_MODEL` value is nonblank after trimming, in registry order. Captured on every provider; absent or null equals `[]`.

Model dispatch, including user mappings and gateway naming, follows [model-policy.md: Session model and stage defaults](model-policy.md#session-model-and-stage-defaults).

**Other inputs (optional unless noted):**

- `scopeAnswer` — `"light"` or `"full"`, stamped only when the 2e trivial-scope gate asked (see 2e).
- `contextLines` / `contextChars` / `contextMaxLineChars` — the shared context file's measured size and maximum line length (including LF) from `write_shared_context.py`'s receipt. **Not provenance — consumed** by the Read plan. Optional (absent ⇒ count-free read-to-end wording); all present measurements must be positive integers. Missing `contextLines` produces one count-free-read gap even if other measurements were copied.
- `contextReadPlanVersion` / `contextReadChunks` — version `1` and a short list of positive per-chunk line counts from the writer. Offsets follow from cumulative counts, whose sum must equal `contextLines`; per-chunk character counts are not copied. Missing or invalid data, including an empty list for a non-empty file, yields one disclosed fallback. Aggregate-only receipts remain compatible with a gap because they cannot be distinguished from a current receipt with both plan fields lost. Every measured chunk is retained up to the shared 2,000-chunk ceiling; compact variable plans are bounded at 26,400 characters plus the context path.
- `discoveryPlanRef` — only the preparation writer's filename and SHA-256. The plan remains on disk. A pinned script computes citation-only coverage and chooses the hunter's regions and facts within 24,000 bytes and 750 lines, including assignment reads. Executor inputs use checked chunks of at most 3,000 characters; each checked response is at most 4,096 characters. Usually selection adds one Sonnet executor call, plus one finalization call when the hunter runs; large citation/risk payloads add transport chunks. Missing or invalid references leave primary review running with coverage unavailable. The trace contains a bounded summary and a SHA-256 reference to the full coverage ledger artifact. Inline plans are not accepted. Existing `removedTests` and read-plan fields retain their current handoff.
- `changedFilesPath` — `{output_dir}/code-gauntlet-files-{head_sha_short}.json`, the on-disk companion to `changedFiles`. Optional provenance only — the workflow has no disk access and never opens it.
- `baseBranch` — the base branch name (verify/blame).
- `unsupportedReviewConfig` — copy `receipt.unsupported_review_config`; a boolean controlling only the local retired-settings notice.
- `persist` - optional, `{ returnPrimaries: true }`. Stamp it to use the RETURN channel. The workflow derives the fallback assemble command from `pluginRoot`; omit both script-path fields. With `returnPrimaries: false`, the artifact-writer transcribes primaries and the executor derives post-review and checkpoint artifacts. Without `persist`, the legacy by-value writer remains available.

**`verify` handoff (sha-scoped paths for the executor's pinned command):**

```
verify: {
  targetKind: "{pr|mr|branch|local}",
  inputPathBase: "{output_dir}/code-gauntlet-phase4-input-{head_sha_short}",
  outputPathBase: "{output_dir}/code-gauntlet-phase4-output-{head_sha_short}"
}
```

Supply `targetKind` and the path bases; the skill cannot pre-write findings that Discover and Merge have yet to produce. See [validation pipeline](validation-pipeline.md#verify-stage-verifystage) for slicing, the wire and trust checks.

**`checkpoints` (resume only):** omit on a fresh run. On resume, copy the whole producer object, `checksum` included, from `return.checkpoints` or the file at `artifactPaths.checkpoints`; never edit it. See [crash recovery](crash-recovery.md#3-checkpoint-resume-fallback-when-resumefromrunid-is-unavailable) for refusal handling.

---

## Triage Announcement

Announce triage results before proceeding: PR title, review mode, file counts by risk level, AI-generated files if any, active dimensions, incremental scope (`Full`, or `Incremental since {last_reviewed_sha} (N commits)` when 2b-post step 3 resolved Incremental — `Full` is the no-op case for a normal review).

For 1000+ line PRs, add: 'This PR is [N] lines; consider splitting it into smaller PRs.'

If the collector receipt (2d) has `gaps[]`, announce each as a one-line note. Also announce every `sources[]` entry with `modified_in_diff: true`; a change to rules governing this review is disclosed, not blocked.

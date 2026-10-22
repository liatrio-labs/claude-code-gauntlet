# Phase 2 Triage Reference

Sub-steps, detection logic, and **args preparation** for Phase 2: Target, Triage & Args Preparation.

## Contents

- **2a** VCS platform detection — **2b** Working tree checkout — **2b-post** SHA + previously-reviewed gate + stale cleanup — **2c** Review target + diff/changed-files save
- **2d** Project context (CLAUDE.md, REVIEW.md parse) — **2e** Risk classification
- **2g** Test discovery — **2h** Docs/specs
- **2k** AI-generated code detection — **2l** Review dimensions
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

**PR/MR mode (`pr_number` set):** use these literal status sections. The producer validates both fields before emitting either.

GitHub:

```bash
echo "=== status ==="
PR_STATUS=$({python} "{plugin_root}/scripts/pr_status.py" --platform github --pr {pr_number}) || exit 1
TARGET_SHA=${PR_STATUS%%$'\n'*}
TARGET_SHA=${TARGET_SHA#head_sha=}
```

GitLab:

```bash
echo "=== status ==="
PR_STATUS=$({python} "{plugin_root}/scripts/pr_status.py" --platform gitlab --pr {pr_number}) || exit 1
TARGET_SHA=${PR_STATUS%%$'\n'*}
TARGET_SHA=${TARGET_SHA#head_sha=}
```

- **Branch comparison:** resolve the target branch with this bare quoted heredoc. Branch names are data, never shell words:

```bash
{python} "{plugin_root}/scripts/resolve_branch.py" <<'CODE_GAUNTLET~BRANCH'
<branch name exactly as the user gave it>
CODE_GAUNTLET~BRANCH
```

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

Runs the gate documented in `phase1-preflight.md` → "Previously-Reviewed Gate", which resolves whether this PR/MR was already reviewed and whether the incremental path is safe. Pass the PR's **own URL** to `--pr-url` so the detector uses the base repository and its host. For both forges, the matching status section above already fetched the URL. Use this literal section in the same composite:

```bash
echo "=== pr_url ==="
PR_URL=${PR_STATUS#*pr_url=}
printf '%s\n' "$PR_URL"
```

This runs **here, not in Phase 1**: the gate compares the last-reviewed commit against the PR head and counts the commits between them, so it needs the working tree at the review target and the PR's objects fetched. Before checkout it would measure whatever branch the session started on. By this point the checkout section has already switched the working tree to the PR; a Skip answer below stops the review but does not revert the checkout — the tree stays checked out on the PR branch.

If the gate resolves to **Incremental**, store `last_reviewed_sha` — section 2c's incremental branch consumes it. If it resolves to **Skip**, stop the run here. Otherwise continue as a full review.

**4. Truncate stale files** with `scripts/stale_truncate.py` so a re-run does not blend old artifacts with new. If it prints `DEFERRED`, run the `--unconditional` form only after a "Yes — review again" answer; a "No — skip" answer stops with the files intact. The gate rationale and outcomes live in `scripts/gauntlet/stale.py`'s docstring.

---

## 2c. Identify Review Target

Use `target_type` and `pr_number` from Phase 1's "Resolve review target" step. Do not re-derive the PR number here.

1. **PR/MR mode** (`pr_number` set) — Use `gh pr view {pr_number}`/`glab mr view {pr_number}` + diff commands. Get full SHA: `git rev-parse HEAD`
   - **GitHub (PR):** Gather the file list with `{python} "{plugin_root}/scripts/list_pr_files.py" --platform github --pr {pr_number}`. Gather the full diff with `gh pr diff {pr_number}`.
   - **GitLab (MR):** Gather the file list with `{python} "{plugin_root}/scripts/list_pr_files.py" --platform gitlab --pr {pr_number}` (the `files` producer in [phase2-preparation.md: Phase 2 Composite B](phase2-preparation.md#phase-2-composite-b--gather-diff-changed-files-line-count-misc)). Gather the full diff with `glab mr diff {pr_number}`.
2. **Branch comparison** - use 2b's resolved SHAs. Save the changed-file JSON first, then the validated patch:

   ```bash
   if ! {python} "{plugin_root}/scripts/list_pr_files.py" --git-range "{base_sha}...{target_sha}" > "{output_dir}/code-gauntlet-files-{head_sha_short}.json"; then
     echo "files: FAILED"
     exit 1
   fi
   command cat "{output_dir}/code-gauntlet-files-{head_sha_short}.json"
   ```

   ```bash
   if ! {python} "{plugin_root}/scripts/save_git_diff.py" --git-range "{base_sha}...{target_sha}" --output "{output_dir}/code-gauntlet-diff-{head_sha_short}.patch"; then
     echo "diff: FAILED"
     exit 1
   fi
   ```

3. **Local changes** - save the changed-file JSON first, then the validated patch:

   ```bash
   if ! {python} "{plugin_root}/scripts/list_pr_files.py" --git-range "HEAD" > "{output_dir}/code-gauntlet-files-{head_sha_short}.json"; then
     echo "files: FAILED"
     exit 1
   fi
   command cat "{output_dir}/code-gauntlet-files-{head_sha_short}.json"
   ```

   ```bash
   if ! {python} "{plugin_root}/scripts/save_git_diff.py" --git-range "HEAD" --output "{output_dir}/code-gauntlet-diff-{head_sha_short}.patch"; then
     echo "diff: FAILED"
     exit 1
   fi
   ```

4. **Incremental** (2b-post step 3's gate resolved "Incremental"; PR/MR mode only) — use branch 1's full server file list saved below to bound the diff:

   ```bash
   {python} "{plugin_root}/scripts/incremental_diff.py" --files "{output_dir}/code-gauntlet-files-{head_sha_short}.json" --since {last_reviewed_sha} > "{output_dir}/code-gauntlet-diff-{head_sha_short}.patch"
   ```

   An unbounded diff would include unrelated files from a base-branch merge. A base merge that touches a PR file still appears. Validate the saved patch as below. If the command fails or the patch is empty, save branch 1's full server diff instead and disclose the fallback. Record `last_reviewed_sha` in the triage announcement and Phase 8 methodology.

On `files: FAILED`, stop the handoff and report the stderr reason (headless: non-zero outcome).

**Save the diff and the changed-file list (the workflow has no git access):** Persist both git-derived inputs to disk so the workflow can consume them.

1. **Changed files** → `{output_dir}/code-gauntlet-files-{head_sha_short}.json` as a JSON array (this path becomes `args.changedFilesPath`). Keep the same array inline for `args.changedFiles` — the Summarize stage reads it by value, because the workflow cannot open the file. On the incremental path, this stays branch 1's full server file list unchanged — the same list branch 4 uses to bound the diff, never a narrower incremental-only set. Saved **first**, so branch 4's incremental diff (below) has a file list already on disk to bound against.
2. **Diff** → `{output_dir}/code-gauntlet-diff-{head_sha_short}.patch`. In PR/MR mode use the server-computed, fork-safe diff (or, when 2b-post step 3 resolved incremental, branch 4's `incremental_diff.py` command); for branch/local targets use `save_git_diff.py` from steps 2/3, which refuses empty or malformed patches. This path becomes `args.diffPath` and is passed to the verify executor as `--diff-file`.

**Run these saves as [phase2-preparation.md: Phase 2 Composite B](phase2-preparation.md#phase-2-composite-b--gather-diff-changed-files-line-count-misc)** — one Bash call. `files` runs first and gates the rest: the incremental diff reads its list, and `numstat` reads the saved patch. That section owns the exact script.

`changedLines` (threaded into `args.changedLines`, Args Preparation below) is counted from the diff actually saved above — the incremental diff on the incremental path, never branch 1's full-PR diff. The `numstat` section runs `scripts/diff_numstat.py` against that saved patch; `scripts/gauntlet/numstat.py`'s docstring holds the counting rules. Check `binary_files > 0` before treating a low count as no diff. Never run a second `gh pr diff`; the 2k scan reads the same saved patch, never a fresh diff fetch.

For PR/MR server diffs, validate the saved diff before relying on it. Branch/local validation is enforced by `save_git_diff.py`:

- Non-empty (file size > 0)
- Starts with `diff --git` (confirms it is a valid unified diff, not an error message)

If `gh pr diff` fails (e.g., 20K-line / 300-file API limit exceeded), the workflow's verify executor falls back to its own git diff chain inside an executor subagent. For branch/local targets, use the diff and file-list commands in steps 2/3 above.

Check for `docs/`, `specs/`, and `research/` directories. Use only the collector receipt from 2d for REVIEW.md discovery and its output for CLAUDE.md/AGENTS.md/QODO.md rules.

---

## 2d. Gather Project Context

1. **Project-rule inputs.** The script below collects CLAUDE.md, AGENTS.md, QODO.md, and REVIEW.md for shared context; Read REVIEW.md separately only for the raw waist in step 3, and never Read or Glob CLAUDE.md/AGENTS.md/QODO.md directly here.
2. **Run the collector first**, after 2c saves the changed-file list, as a standalone script call:

   ```bash
   {python} "{plugin_root}/scripts/collect_project_rules.py" --repo-root "$(git rev-parse --show-toplevel)" --out "{output_dir}/code-gauntlet-project-rules-{head_sha_short}.md" --changed-files "{output_dir}/code-gauntlet-files-{head_sha_short}.json"
   ```

   Parse its one-line stdout JSON receipt and retain it; stop on failure instead of stamping a successful empty discovery. The script owns the repo-root, changed-file-directory, and ancestor walk. REVIEW.md text is copied with source provenance into review-rules blocks before project-rules blocks; the script does not parse its config fence. PROJECT_RULE_FILENAMES controls CLAUDE.md/AGENTS.md/QODO.md discovery, and REVIEW_RULE_FILENAME names the separate REVIEW.md source kind.
   Project-rule imports resolve relative to their containing file, within the repository, to Markdown targets, up to four hops; inline imports are recognized outside code spans and fences. The review pass does not follow imports or deduplicate scopes by content. A project import of a realpath already rendered as review-rules records `review_rules_source` and stops at that target. Direct project walk hits remain eligible. Other project imports, including out-of-walk REVIEW.md targets, remain project rules and do not add review scopes.
   Project rules remain additive: more specific directories win on conflict, with CLAUDE.md before AGENTS.md before QODO.md at equal specificity. REVIEW.md settings retain the separate pipeline precedence in `references/review-md-spec.md`.
3. **Stamp REVIEW.md raw inputs from the receipt.** Read exactly the paths in `receipt.review_md`, in receipt order, and stamp `args.reviewMd` as `[{ path, text, checksum }, ...]`, using each repo-relative path and the full raw file text, copying `checksum` from its receipt entry and `args.reviewMdSetChecksum` from `receipt.review_md_checksum`. Do not sort, walk directories, or parse the files yourself. An empty list stamps `[]`, an authoritative "discovered nothing", not an omitted field. Keep the text by value because the workflow has no filesystem. Set `reviewConfigPath` to the root REVIEW.md path when the list contains `REVIEW.md`, otherwise null.
   A capped REVIEW.md remains listed: Read its full raw text for settings, retain the cap gap, and do not add its text separately to the shared context. A failed raw Read stops this handoff; never replace it with empty text. See `references/review-md-spec.md` for format and hierarchy; `resolveReviewConfig` owns parsing and subtree settings.

Do not `Glob(pattern: "**/REVIEW.md")`; use the collector's receipt, not a second search. Never use `find` from Bash for locating these files. Never Read or Glob CLAUDE.md/AGENTS.md/QODO.md directly here.

### REVIEW.md Detection

Complete 2d's collector and raw-input handoff before proceeding to 2e. REVIEW.md thresholds and ignore patterns apply to matching finding subtrees through the pipeline. Every context-reading agent receives all source-path-tagged `review-rules` blocks; each block's prose is advisory for its source directory's subtree.

A successful empty discovery never blocks or asks a question. Collector or raw Read failures stop the handoff.

Pass raw `args.reviewMd` as described in [phase2-preparation.md: Discover REVIEW.md and stamp its raw text](phase2-preparation.md#discover-reviewmd-and-stamp-its-raw-text). The pipeline merges thresholds and ignore patterns per matching finding subtree; the shared context retains every source-path-tagged prose block as advisory guidance for its source subtree. Never hand-parse here.

> Headless mode has identical behavior. REVIEW.md is read-only in both modes. See
> `references/headless-mode.md`.

---

## 2e. Classify Changed Files by Risk Level

- **High risk** — auth, security, payment, data access, public APIs, DB migrations, crypto, infra/deploy, permission/RBAC. Also >200 lines changed. Also: files implementing a cache, proxy, decorator, or delegation pattern (caching proxies are a common source of recursive delegation and stale-data bugs — flag these even if the diff appears mechanical).
- **Medium risk** — business logic, services, controllers, middleware, state management. 50-200 lines changed.
- **Low risk** — tests, docs, config, generated code, lockfiles, formatting-only. <50 lines changed.

High-risk files get expanded context (callers, callees, related tests); low-risk get lighter review.

**Content-change promotion.** After initial classification, check LOW-risk files for substantive content changes — any diff line that changes a string value, numeric value, or identifier (not just formatting, whitespace, markup, or delimiters). Files with substantive content changes get promoted to MEDIUM. This is type-agnostic.

Promotion triggers: i18n text changes, config value changes, CSS/SCSS numeric changes, changed string literals or identifiers.
Stay LOW: lock files, whitespace-only changes, generated code updates, tag case changes (`<br/>` → `<br />`).

### Light Review for Trivial PRs

If ALL files are low-risk AND total lines <50, ask Light review vs Full review (template in `references/phase1-preflight.md`). There is no REVIEW.md key that skips this question — dimension selection is not REVIEW.md-configurable (`references/review-md-spec.md` → "Rules and other prose"). Stamp the answer verbatim into `args.scopeAnswer` (`"light"` or `"full"`) — the workflow itself computes the dimension flags from `scopeAnswer` together with the `riskTable`/`changedLines` you are already stamping (`deriveAgentFlags`, `workflows/src/discover.js`): a `light` answer runs only the two core agents (`bug-detector`, `security-reviewer`); `full` runs all seven discovery agents covering nine dimensions. Announce the actual dimension set — `bugs, security` for light, the full list for full. `args.riskTable` is REQUIRED on every run regardless of the answer (or of whether this question was even asked) — see Args Preparation below.

> Headless exception: do not ask, and leave `scopeAnswer` out; interactive runs stamp the question answer.

Only stamp `scopeAnswer` when this gate actually fired (every file low-risk AND total lines <50); omit it entirely otherwise — the args waist refuses a `scopeAnswer` the riskTable/changedLines don't support, and refuses a light-eligible riskTable with no `scopeAnswer` at all (the gate must have asked).

---

## 2g. Related Test Discovery

For each changed production file, use **Glob** to find related tests without filtering by language
or extension. Select paths with a case-insensitive directory segment `test`, `tests`, `spec`,
`specs` or `__tests__`; or basename tokens `test_*`, `*_test`, `*_spec`, `*.test.*`, `*.spec.*`,
`*Test`, `*Tests`, `*Spec` (stem before the first dot). Exclude non-source directories such as
vendor, node_modules, third_party, deps and generated output before selection.
Include related tests in context for bug-detector and test-analyzer. The saved-diff inventory
uses the old path for removed and renamed tests.

Example:

```text
Glob(pattern: "**/*.test.*")
Glob(pattern: "**/*_test*")
Glob(pattern: "**/test_*")
Glob(pattern: "**/__tests__/**/*")
Glob(pattern: "**/tests/**/*")
```

Never use `find` or `grep` from Bash for test discovery.

---

## 2h. Docs/Specs Context

If `docs/`, `specs/`, `research/` exist, read relevant files. Send only to the conventions-and-intent agent — NOT all agents (avoids biasing toward confirming intent rather than finding bugs).

**Tool instructions for file discovery:**

Use **Glob** to find documentation and specification files:

```
Glob(pattern: "docs/**/*.md")
Glob(pattern: "specs/**/*.md")
Glob(pattern: "research/**/*.md")
```

Then use **Read** to load relevant files for each changed file's directory. Never use `find` from Bash for locating docs/specs.

---

## 2k. AI-Generated Code Detection

Scan for AI co-author trailers, attribution comments, AI tool metadata. **Elevate AI-generated files one risk level** (research shows 75% more logic errors in AI-authored code). Include AI-generation status in risk classification sent to all agents.

**Tool instructions:**

Use **Grep** to search for AI co-author indicators in changed files:

- Git trailers: `Co-Authored-By`, `Co-authored-by`, `Copilot-By`
- Comments: patterns like `AI-generated`, `generated by`, `GPT`, `Claude`, `Copilot`, `ChatGPT`
- Metadata: language-specific markers (e.g., `<!-- AI generated -->`, `# AI generated`)

Example:

```
Grep(pattern: "Co-[Aa]uthored-[Bb]y|Copilot-By", type: "text", glob: "**/*.py")
Grep(pattern: "AI-generated|generated by (GPT|Claude|Copilot|ChatGPT)", glob: "**/*.{js,ts,py}")
Grep(pattern: "<!-- AI|# AI generated|// AI generated", glob: "**/*.{js,ts,py,md,html}")
```

Also run the same patterns directly against the saved diff — `Grep(pattern: "...", path: "{output_dir}/code-gauntlet-diff-{head_sha_short}.patch")` — to catch markers in added lines the working-tree scan above might miss. This patch is already on disk from Composite B's `diff` section (2c); never invoke a fresh `gh pr diff` here to re-fetch it.

Never use `grep` or `find` from Bash for AI detection.

---

## 2l. Determine Review Dimensions

The workflow computes the dispatched dimensions with `deriveAgentFlags(riskTable, changedLines, scopeAnswer)` in `workflows/src/discover.js`.
A `light` answer on a light-eligible diff (every changed file low-risk and fewer than 50 changed lines) yields `{ deep: false }` and dispatches only bug-detector and security-reviewer.
Every other valid run dispatches all seven discovery agents.
The default discovery model policy uses Sonnet except security-reviewer, which uses Opus; a `policy.subagentModel` override applies to every stage.

---

## Shared Agent Context File

The workflow's **summarize, discovery, and validate** agents Read a shared context file. The workflow threads exactly this path to them: `{output_dir}/code-gauntlet-context-{head_sha_short}.md`. The skill must write it there before the Phase 3 `Workflow` call, or the agents' "Read the shared context" step hits a missing file. The **challenger** is not in that list and never has been — it is structurally blind (it receives only a finding's title, description, and location and opens the code itself), so the Challenge stage is given no context path at all.

Use the command in [phase2-preparation.md: Write the shared agent context file](phase2-preparation.md#write-the-shared-agent-context-file) and `scripts/write_shared_context.py`; pass the 2e risk classification and 2k AI-generated-code status as Markdown on stdin.

The **change summary** is no longer written into the context file — the workflow's Summarize stage produces it internally and threads it to `renderReport()` for its Change Context section. The NDJSON `## Validator` section is likewise dropped: v3 agents return findings through structured output, not by appending NDJSON, so there is no per-agent validator step to record.

### Measure it

Stamp the script receipt verbatim. Both waist fields are optional only so a skipped step degrades to a disclosed `context_unmeasured` gap, never a licence to skip; `contextChars` without `contextLines` is refused at the waist. `contextReadPlan` and `sharedContextLine` (`workflows/src/contextPlan.js`) turn the pair into the Read calls.

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
- `web_origin` (required): an http(s) origin: scheme, host and optional port only.
- `title` (optional): a non-empty string when present.

Producer commands:

`gh pr view {pr_number} --json url,title | {python} "{plugin_root}/scripts/resolve_pr_identity.py" --platform github --sha "$(git rev-parse HEAD)"`

`glab mr view {pr_number} --output json | {python} "{plugin_root}/scripts/resolve_pr_identity.py" --platform gitlab --sha "$(git rev-parse HEAD)"`

The title and URL reach the script only through the pipe.
<!-- /generated-from-registry-identity:pr_identity_fields -->

**Omit optional fields you have no value for; never stamp an explicit `null`.**
The waist treats `null` as absent for `reviewConfig`, `exclusionPatterns`, `delivery`, and `checkpoints`.
Keep `reviewConfigPath: null` when the receipt has no root REVIEW.md, even if it lists subdirectory files; this field records root provenance.

The workflow derives the host platform from `repoRoot`, `outputDir`, and `pluginRoot`; any drive-form root selects Windows command limits.

**Required fields (`validateArgs` fails loud without them):**

| Field | Value |
|---|---|
| `argsVersion` | `1` |
| `mode` | Copy `configResult.mode` from the resolver JSON |
| `repoRoot` | `configResult.identity.repo_root` (Python-resolved wire path) |
| `outputDir` | resolved `{output_dir}` (absolute) |
| `headShaShort` | `head_sha_short` from 2b-post |
| `nonce` | freshly generated, matching `^[A-Za-z0-9._-]+$` (interpolated into the verify executor argv per slice — no whitespace/shell metacharacters) |
| `generatedAt` | current wall-clock as an ISO8601 string — the workflow's injected clock (it never calls `new Date()`) |
| `diffPath` | `{output_dir}/code-gauntlet-diff-{head_sha_short}.patch` |
| `changedFiles` | the changed-file array, by value (Summarize bucketing; the workflow has no disk access) |
| `changedLines` | total changed line count, by value (Summarize bucketing threshold) |
| `riskTable` | the Phase 2e per-file risk classification, by value, as `[{ path, risk }]` — `path` set must equal `changedFiles` exactly (missing or extra paths fail loud; see Phase 2e's risk-level table for the `risk` contract) |
| `configEcho` | Copy `configResult.waist` verbatim into args, including all fields it carries. |
| `pluginRoot` | `configResult.identity.plugin_root`; the workflow derives its script paths from this root. |
| `python` | Phase 1 selection: `python3`, `python`, or `py -3`; optional, defaults to `python3`. |
| `reviewScope` | Headless PR/MR targets omit `requested` (see the derived waist fields under "Args Preparation"). Interactive PR/MR targets stamp the gate answer, or `full` when no prior review exists. Local and branch targets stamp `full`. Copy detector facts verbatim for PR/MR targets. Use `kind=incremental` only for an incremental request with `detector.incremental_safe=true`. Set `since` to the detector's safe `last_reviewed_sha` only for incremental kind. Set `since` to `null` otherwise. Retain detector facts when an incremental request becomes full. The renderer computes the fallback reason from retained detector facts. |
| `policy` | `{ tier, subagentModel, provider, gateway, mappedAliases }` — see below |
| `limits` | Stamp `{}` unless a genuine REVIEW.md-set override exists. |
| `delivery` | For PR/MR targets, stamp only `{ prIdentity }` using the generated field contract above. Omit it for local targets. |

<!-- generated-from-registry-identity:derived_waist_fields — do not edit; run scripts/generate_contract_requirements.py -->
The workflow derives these waist fields from the copied `configEcho` receipt. Do not stamp a derived field; when a listed derivation applies, a stamped value that disagrees with its receipt is refused before dispatch.

- `limits.deliveryCap` (headless and interactive runs): from `configEcho.pr_comment_cap`; digits derive as a JSON number; on interactive runs the receipt spelling `null` derives as JSON `null`. Stamp `limits` and leave `deliveryCap` out of it.
- `delivery.tier` (headless and interactive runs): from `configEcho.delivery_tier`. Leave `tier` out of any stamped `delivery`.
- `reviewScope.requested` (headless runs, when `reviewScope.detector` is an object): from `configEcho.reviewed_policy`; `skip` derives as `full`. Stamp `reviewScope` and leave `requested` out of it.
- `scopeAnswer` (headless runs, when every changed file is low risk and fewer than 50 lines changed): from `configEcho.trivial_scope`. Leave it out.

The workflow fills these receipt entries itself; never stamp them.

- `configEcho.review_md` (interactive runs): `present` when the `reviewMd` array is nonempty, else `absent`; without `reviewMd`, `present` when `reviewConfigPath` is set, else `absent`.
<!-- /generated-from-registry-identity:derived_waist_fields -->

The Challenge stage applies the receipt-backed tier and cap before Phase 8 posts `artifactPaths.postReview` verbatim.

**`policy` (model policy the workflow runs under):**

Run `scripts/capture_policy_env.py` and stamp its four keys verbatim. The workflow cannot read `process.env`.

- `tier` - copy `configResult.resolved.model_tier`; the resolver owns its value and source.
- `subagentModel` - `CLAUDE_CODE_SUBAGENT_MODEL`, or `null` when unset or blank. If non-null, warn the user and record the override in the methodology.
- `provider` - first truthy switch in Bedrock, Vertex, Foundry order (`CLAUDE_CODE_USE_<PROVIDER>`), else `firstParty`. Truthy means `1`/`true`/`yes`/`on`, trimmed and case-insensitive. Other provider spellings are rejected at the waist.
- `gateway` - stamp `policy.gateway` as `true` iff `ANTHROPIC_BASE_URL` is nonblank after trimming. It leaves `provider` unchanged and disables conditional per-dimension schemas because gateways can forward schemas to another backend. Absent or null equals `false`.
- `mappedAliases` - distinct model aliases whose `ANTHROPIC_DEFAULT_<ALIAS>_MODEL` value is nonblank after trimming, in registry order. Captured on every provider; absent or null equals `[]`.

Model dispatch, including user mappings and gateway naming, follows [model-policy.md: Session model and stage defaults](model-policy.md#session-model-and-stage-defaults).

**Other inputs (optional unless noted):**

- `scopeAnswer` — `"light"` or `"full"`, the 2e trivial-scope gate's answer, stamped ONLY when that gate actually asked (every changed file low-risk and `changedLines < 50`; see 2e). Omit otherwise — the workflow refuses a `scopeAnswer` incoherent with `riskTable`/`changedLines` (any present `scopeAnswer` when the riskTable is not light-eligible, or a light-eligible riskTable with no `scopeAnswer` at all).
- `contextLines` / `contextChars` — the shared context file's measured size from `write_shared_context.py`'s receipt. **Not provenance — consumed.** `contextReadPlan` turns them into the exact `Read` calls the Summarize/Discover/Validate prompts enumerate, so the agent is told which calls to make instead of having to notice an unannounced truncation. Both optional (absent ⇒ count-free read-to-end wording); `contextChars` requires `contextLines`; both must be positive integers.
- `changedFilesPath` — `{output_dir}/code-gauntlet-files-{head_sha_short}.json`, the on-disk companion to `changedFiles`. Optional provenance only — the workflow has no disk access and never opens it.
- `baseBranch` — the base branch name (verify/blame).
- `reviewMd` / `reviewMdSetChecksum` - `[{ path, text, checksum }, ...]` plus `receipt.review_md_checksum` in collector receipt order (2d step 3), the raw text of every REVIEW.md listed by the receipt. Never hand-parse it here: the workflow's `resolveReviewConfig` (`workflows/src/args.js`) builds root and subtree layers before the Filter stage selects settings by finding file. An empty array is a legal, authoritative "discovered nothing."
- `exclusionsText` — the raw text of whatever exclusions source was found (e.g. `.reviewignore`), parsed by `resolveReviewConfig` via `loadExclusions` (`workflows/src/reviewConfig.js`).
- `reviewConfig` / `exclusionPatterns` — the LEGACY pre-parsed form. Still accepted for backward compatibility (bench children, older callers), but do not stamp these alongside `reviewMd`/`exclusionsText` for the same axis — the waist rejects a waist that stamps both the raw and pre-parsed form (single authority).
- `reviewConfigPath` - the root REVIEW.md path (or `null`), carried for provenance. When `reviewMd` is an array, this must be non-null iff an entry has path exactly "REVIEW.md"; legacy pre-parsed callers retain their existing path provenance.
- `persist` - optional, `{ returnPrimaries: true }`. Stamp it to use the RETURN channel. The workflow derives the fallback assemble command from `pluginRoot`; omit both script-path fields. With `returnPrimaries: false`, the artifact-writer transcribes primaries and the executor derives post-review and checkpoint artifacts. Without `persist`, the legacy by-value writer remains available.

**`verify` handoff (sha-scoped paths for the executor's pinned command):**

```
verify: {
  inputPathBase: "{output_dir}/code-gauntlet-phase4-input-{head_sha_short}",
  outputPathBase: "{output_dir}/code-gauntlet-phase4-output-{head_sha_short}"
}
```

Supply the path bases and any REVIEW.md `verifySliceSize` override. Inside the workflow, Verify projects merged findings to location/severity/confidence scalars, true-only reference and removed-test flags, and sorted symbols. It plans slices under `VERIFY_INLINE_CHAR_BUDGET` and dispatches one executor per slice. The executor copies the pinned command's inline token exactly; Python decodes it, writes `--input`, verifies, and returns a receipt with per-record proofs and deltas. Held findings retain prose and original references after the join. The shared base branch and delta checksum gate the whole attempt; matching record proofs accept individual decisions. Only unresolved findings retry once in original order with a smaller token and nonce `{nonce}.{i}.r1`; only those still unresolved degrade. The skill cannot pre-write findings that Discover and Merge have yet to produce. See [validation pipeline](validation-pipeline.md#verify-stage-verifystage) for the wire and trust checks.

**`checkpoints` (resume only):** omit on a fresh run. On resume, copy the whole producer object, `checksum` included, from `return.checkpoints` or the file at `artifactPaths.checkpoints`; never edit it. See [crash recovery](crash-recovery.md#3-checkpoint-resume-fallback-when-resumefromrunid-is-unavailable) for refusal handling.

---

## Triage Announcement

Announce triage results before proceeding: PR title, review mode, file counts by risk level, AI-generated files if any, active dimensions, incremental scope (`Full`, or `Incremental since {last_reviewed_sha} (N commits)` when 2b-post step 3 resolved Incremental — `Full` is the no-op case for a normal review).

For 1000+ line PRs, add: 'This PR is [N] lines; consider splitting it into smaller PRs.'

If the collector receipt (2d step 2) has `gaps[]`, announce each as a one-line note. Also announce every `sources[]` and `review_md[]` entry with `modified_in_diff: true`; a change to rules governing this review is disclosed, not blocked.

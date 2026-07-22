# Phase 2 Triage Reference

Sub-steps, detection logic, and **args preparation** for Phase 2: Target, Triage & Args Preparation.

## Contents

- **2a** VCS platform detection — **2b** Working tree checkout — **2b-post** SHA + gitignore + stale cleanup — **2c** Review target + diff/changed-files save
- **2d** Project context (CLAUDE.md, REVIEW.md parse) — **2e** Risk classification
- **2g** Test discovery — **2h** Docs/specs — **2i** History context
- **2k** AI-generated code detection — **2l** Review dimensions
- **Shared agent context file** — **Args preparation** (the args waist the workflow consumes)
- **Triage announcement**

> **What moved into the workflow (v3).** The change summarizer (v2 steps **2f** and **2j**) is now the workflow's **Summarize** stage — Phase 2 no longer dispatches summarizer agents. The old two-batch "agents then file discovery" execution strategy is likewise gone; Phase 2 is now a straight-line context-and-args build, and the only agent dispatch is the single `Workflow` call in Phase 3. The 2f/2j content below is retained only to explain what the Summarize stage now does internally.

---

## 2a. Detect VCS Platform

Auto-detect from `git remote get-url origin`:

- GitHub → `gh` CLI, "PR"
- GitLab (including self-hosted) → `glab` CLI, "MR"

If detection fails, ask the user.

---

## 2b. Ensure Working Tree Reflects Review Target

Before running any diff commands, confirm the local working tree matches the review target. Use the `pr_number` resolved in Phase 1 — never extract PR numbers from branch names (branch names may contain upstream PR numbers that differ from the PR number in the current repo).

**1. Resolve the target's head SHA:**

- **PR/MR mode (`pr_number` set):** `gh pr view {pr_number} --json headRefOid --jq '.headRefOid'` (GitHub) / `glab mr view {pr_number} --output json | jq '.sha'` (GitLab)
- **Branch comparison:** `git rev-parse <branch>`
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
| Branch name | `git checkout <branch>` |
| Local changes | no-op |

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): never run any checkout command from
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

## 2b-post. Resolve Head SHA, Gitignore, and Clean Stale Files

Now that the working tree reflects the review target, compute the short SHA and perform housekeeping. These steps run after checkout so the SHA reflects the actual PR HEAD and the gitignore addition is not stashed by `gh pr checkout`.

**1. Resolve head SHA:**

```bash
Bash(command="git rev-parse --short=8 HEAD")  # Store as `head_sha_short`
```

Computed after checkout so the SHA reflects the actual PR HEAD, not whatever branch was checked out before.

**2. Ensure `{output_dir}` is gitignored** (skip if using env var override):

```bash
Bash(command="git check-ignore -q .code-gauntlet 2>/dev/null || echo '/.code-gauntlet/' >> .gitignore")
```

Added after checkout to avoid stash/pop loss from `gh pr checkout` — if this ran before checkout, the gitignore modification would be stashed and potentially lost.

**3. Truncate stale files** from prior sessions with the same SHA:

```bash
Bash(command="python3 -c \"import glob; [open(f,'w').close() for f in glob.glob('{output_dir}/code-gauntlet-*-{head_sha_short}.*')]\"")
```

Prevents echo-append (`>>`) from accumulating findings across sessions. Without truncation, re-running a review on the same SHA would append duplicate findings to existing NDJSON files.

---

## 2c. Identify Review Target

Use `target_type` and `pr_number` from Phase 1's "Resolve review target" step. Do not re-derive the PR number here.

1. **PR/MR mode** (`pr_number` set) — Use `gh pr view {pr_number}`/`glab mr view {pr_number}` + diff commands. Get full SHA: `git rev-parse HEAD`
   - **GitHub (PR):** Gather the file list with `gh pr diff {pr_number} --name-only`. Gather the full diff with `gh pr diff {pr_number}`.
   - **GitLab (MR):** Gather the file list with `glab mr diff {pr_number} --name-only`. Gather the full diff with `glab mr diff {pr_number}`.
2. **Branch comparison** — `git diff <base>...HEAD` and `git diff --name-only <base>...HEAD`
3. **Local changes** — `git diff HEAD` (or `git diff --cached` if nothing unstaged)

**Save the diff and the changed-file list (the workflow has no git access):** Persist both git-derived inputs to disk so the workflow can consume them.

1. **Diff** → `{output_dir}/code-gauntlet-diff-{head_sha_short}.patch`. In PR/MR mode use the server-computed, fork-safe diff; for branch/local targets use `git diff`. This path becomes `args.diffPath` and is passed to the verify executor as `--diff-file`.
2. **Changed files** → `{output_dir}/code-gauntlet-files-{head_sha_short}.json` as a JSON array (this path becomes `args.changedFilesPath`). Keep the same array inline for `args.changedFiles` — the Summarize stage reads it by value, because the workflow cannot open the file.

```bash
# PR/MR mode: server-computed diff + name-only file list
gh pr diff {pr_number} > "{output_dir}/code-gauntlet-diff-{head_sha_short}.patch"
gh pr diff {pr_number} --name-only  # capture into code-gauntlet-files-{sha}.json as a JSON array
```

Validate the saved diff before relying on it:

- Non-empty (file size > 0)
- Starts with `diff --git` (confirms it is a valid unified diff, not an error message)

If `gh pr diff` fails (e.g., 20K-line / 300-file API limit exceeded), the workflow's verify executor falls back to its own git diff chain — but note that fallback runs inside an executor subagent (which has shell), not in the script. For **branch comparison** and **local changes** target types, produce the diff with `git diff <base>...HEAD` / `git diff HEAD` and the file list with `git diff --name-only`.

Check for `docs/`, `specs/`, `research/` directories and `REVIEW.md`, `CLAUDE.md`, `AGENTS.md`, `QODO.md` at repo root and in directories with changed files.

---

## 2d. Gather Project Context

1. **CLAUDE.md** — Read from repo root and directories with changed files.
2. **REVIEW.md** — Discover hierarchically. See `references/review-md-spec.md` for format, scaffolding templates, and hierarchy rules. REVIEW.md lets maintainers customize focus areas, skip patterns, custom rules, thresholds, and ignore patterns.
3. **AGENTS.md / QODO.md** — Read if present.

**Tool instructions for file discovery:**

Use **Glob** to find all CLAUDE.md, REVIEW.md, AGENTS.md, and QODO.md files:

```
Glob(pattern: "**/CLAUDE.md")
Glob(pattern: "**/REVIEW.md")
Glob(pattern: "**/AGENTS.md")
Glob(pattern: "**/QODO.md")
```

Never use `find` from Bash for locating these files.

### REVIEW.md Detection

Complete this check before proceeding to 2e. REVIEW.md settings cascade to all thresholds, rules, and ignore patterns for the entire review.

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): skip both REVIEW.md-setup prompts below (the "No REVIEW.md found" build-review-md suggestion and the subdirectory-REVIEW.md `AskUserQuestion`). Root config applies to all directories; never invoke `build-review-md`. REVIEW.md is read-only in headless mode — the hierarchical parse still runs, but no REVIEW.md is created. See `references/headless-mode.md`.

Find all CLAUDE.md locations, check each for a matching REVIEW.md:

- **No REVIEW.md anywhere:**

  ```
  No REVIEW.md found. For a guided setup, run build-review-md first, then restart the review. Or continue without one.
  ```

- **Root exists but subdirectory CLAUDE.md has no matching REVIEW.md:**

  ```
  AskUserQuestion(
    questions: [{
      question: "Found REVIEW.md at repo root, but {directory} has a CLAUDE.md without a matching REVIEW.md. A subdirectory REVIEW.md lets you set different review standards for this area. Create one?",
      header: "Subdirectory REVIEW.md",
      multiSelect: false,
      options: [
        { label: "Yes — create it", description: "Inherits root settings, adds directory-specific rules" },
        { label: "Not now — root config applies", description: "Use root REVIEW.md settings for all directories" }
      ]
    }]
  )
  ```

- **All locations covered** → proceed.

See `references/review-md-spec.md` section Discovery for the full prompts and scaffolding templates. Merge configs hierarchically: settings override, rules and patterns accumulate.

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

If ALL files are low-risk AND total lines <50, ask Light review vs Full review (template in `references/phase1-preflight.md`). Skipped when REVIEW.md sets `focus`. In light mode, triage announcement shows `Review dimensions: bugs, security (light review mode)`.

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): do not ask — use `$CODE_GAUNTLET_TRIVIAL_SCOPE` (`light` runs bugs+security only, `full` runs all dimensions). See `references/headless-mode.md`.

---

## 2f. Change Summary (now the workflow's Summarize stage)

The semantic change summary is no longer produced in Phase 2. The workflow's **Summarize** stage dispatches the `change-summarizer` agent internally (its model comes from `resolvePolicy` — Sonnet by default; the `frontier` flag does **not** upgrade it, only the challenger) and threads the result to the report writer. The skill neither dispatches a summarizer nor writes the summary into the context file.

For reference, the Summarize stage produces a 3–5 sentence summary of what the change *claims* to do, its rationale, and its risk profile, framed strictly as claims (never "clean", "correct", "safe", "straightforward", "trivial", or "verbatim" — the summary must never conclude a refactoring is correct). The change-summarizer agent definition holds the authoritative framing rules.

**Large changes.** For >500-line changes that also span more files than one summarize bucket, the Summarize stage fans out per-file buckets through `parallel()` and stitches the partials with a single merge call — again, internal to the workflow, not a Phase 2 dispatch. (This subsumes the old 2j file-level summarization step.)

---

## 2g. Related Test Discovery

For each changed production file, find test files by convention (`Tests`, `.test`, `.spec`, `_test`, `_spec` patterns; `tests/`, `__tests__/`, `spec/` directories). Include in context for bug-detector and test-analyzer.

**Tool instructions:**

Use **Glob** to find test files. Pattern examples:

- `**/*.test.js`, `**/*.test.ts` (Jest/Vitest style)
- `**/*.spec.js`, `**/*.spec.ts` (Jasmine/Mocha style)
- `**/tests/**/*.py`, `**/__tests__/**/*.py` (Python)
- `**/*_test.go`, `**/*_test.rs` (Go/Rust)

Example:

```
Glob(pattern: "**/*.test.{js,ts,py}")
Glob(pattern: "**/__tests__/**/*")
Glob(pattern: "**/tests/**/*")
```

Never use `find` or `grep` from Bash for test discovery.

---

## 2h. Docs/Specs Context

If `docs/`, `specs/`, `research/` exist, read relevant files. Send only to conventions-and-intent agent and Phase 8 report generation — NOT all agents (avoids biasing toward confirming intent rather than finding bugs).

**Tool instructions for file discovery:**

Use **Glob** to find documentation and specification files:

```
Glob(pattern: "docs/**/*.md")
Glob(pattern: "specs/**/*.md")
Glob(pattern: "research/**/*.md")
```

Then use **Read** to load relevant files for each changed file's directory. Never use `find` from Bash for locating docs/specs.

---

## 2i. History Context Preprocessing

**Deterministic preprocessing, not an LLM agent.** For each changed file:

1. `git log --oneline --max-count=50 -- <file>` for recent change history
2. `git blame` on changed line ranges (used by the Verify stage's `verify_findings.py` executor for new/surfaced classification)

Distribute: bug-detector gets history context; conventions-and-intent gets pattern drift context.

---

## 2j. File-Level Summarization (now internal to the Summarize stage)

Per-file summarization for large changes is no longer a Phase 2 dispatch — it is the bucket fan-out described in 2f above. When the change exceeds 500 lines and spans more files than one summarize bucket, the workflow's Summarize stage fans out one `change-summarizer` call per per-file bucket through `parallel()` and stitches the partials with a merge call. There is no separate 2j step and no "agents then file discovery" batching to arrange — the only agent dispatch the skill makes is the single `Workflow` call in Phase 3.

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

Never use `grep` or `find` from Bash for AI detection.

---

## 2l. Determine Review Dimensions

All on by default unless REVIEW.md disables them. In **Optimized** mode, all agents use Sonnet except security-reviewer (always Opus). In **Frontier** mode the `frontier` flag currently upgrades only the blind challenger (per `resolvePolicy`); discovery agents keep their Optimized models. Wider frontier coverage is research-pending.

Skip conditions: test-analyzer (no test files in repo), type-design-analyzer (no new types).

---

## Shared Agent Context File

The workflow's discovery, validate, and challenge agents Read a shared context file. The workflow threads exactly this path to them: `{output_dir}/code-gauntlet-context-{head_sha_short}.md`. The skill must write it there before the Phase 3 `Workflow` call, or the agents' "Read the shared context" step hits a missing file.

Write it with `python3 -c "import json; ..."`. Contents:

- CLAUDE.md / REVIEW.md project rules.
- Risk classification (2e) and AI-generated-code status (2k).
- The full diff inside `<untrusted-code-content>` tags. Raw diff lines only — never substitute a summary for changed content; evidence destroyed during summarization cannot be recovered by agents.

The **change summary** is no longer written into the context file — the workflow's Summarize stage produces it internally and threads it to the report writer. The NDJSON `## Validator` section is likewise dropped: v3 agents return findings through structured output, not by appending NDJSON, so there is no per-agent validator step to record. (The emission machinery still ships — its removal is the deferred S8 migration.)

---

## Args Preparation

Assemble the args waist the workflow consumes. It is a single JSON object passed as the `Workflow` tool's `args` parameter (Phase 3) — not written to disk. The workflow validates it up front (`validateArgs`) and rejects a malformed waist before any dispatch.

**Required fields (`validateArgs` fails loud without them):**

| Field | Value |
|---|---|
| `argsVersion` | `1` |
| `mode` | `"headless"` under `CODE_GAUNTLET_HEADLESS=1`, else `"interactive"` |
| `repoRoot` | `git rev-parse --show-toplevel` |
| `outputDir` | resolved `{output_dir}` (absolute) |
| `headShaShort` | `head_sha_short` from 2b-post |
| `nonce` | freshly generated, matching `^[A-Za-z0-9._-]+$` (interpolated into the verify executor argv per slice — no whitespace/shell metacharacters) |
| `generatedAt` | current wall-clock as an ISO8601 string — the workflow's injected clock (it never calls `new Date()`) |
| `diffPath` | `{output_dir}/code-gauntlet-diff-{head_sha_short}.patch` |
| `changedFilesPath` | `{output_dir}/code-gauntlet-files-{head_sha_short}.json` |
| `agentFlags` | map of conditional-dimension flags (all nine dimensions are unconditional today, so `{}` unless REVIEW.md gates a future conditional dimension) |
| `policy` | `{ tier, frontier, frontierModelId, subagentModel }` — see below |
| `limits` | `{ summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, schemaFailureLimit: 3, verifySliceSize: 200, deliveryCap }` (override from REVIEW.md if set) |
| `delivery` | `{ tier: "all" \| "main_only" }` — the Phase 8 PR-comment tier (default `all`); optional (absent ⇒`all`) |

`limits.deliveryCap` is the Phase 8 PR-comment cap, threaded from `CODE_GAUNTLET_PR_COMMENT_CAP` (the same knob echoed as `pr_comment_cap`; headless default `6`, bench `25`) — the **workflow cannot read `process.env`**, so passing it through the waist is the only path. `delivery.tier` is the Phase 8 delivery tier from the Phase 1 answer (interactive) or `CODE_GAUNTLET_DELIVERY_TIER` (headless, default `all`); same env-blindness, same reason it rides the waist. The Challenge stage hands every survivor to the workflow's `selectDelivery(survivors, deliveryCap, tier)`, which applies the tier (`all` keeps every survivor, `main_only` keeps main-tagged only), ranks, and keeps the top `deliveryCap` as the persisted post-review payload (`artifactPaths.postReview`) Phase 8 posts verbatim. Omit `deliveryCap` (or leave it `null`) to deliver uncapped; omit `delivery` to default the tier to `all`.

**`policy` (model policy the workflow runs under):**

- `tier` — `"optimized"` or `"frontier"`, from the Phase 1 review-mode answer.
- `frontier` — `false` for Optimized, `true` for Frontier.
- `frontierModelId` — `null` for Optimized; a **full model-id string** for Frontier (`validateArgs` rejects `frontier:true` with a missing id).
- `subagentModel` — read `CLAUDE_CODE_SUBAGENT_MODEL` from the environment (or `null`). **The workflow cannot read `process.env`**, so this capture is the only path for it. If set, warn the user and record it in the methodology — it silently overrides the entire per-stage model policy.

**By-value inputs the in-memory stages need (the workflow has no disk, so paths alone are not enough):**

- `changedFiles` — the changed-file array (Summarize).
- `changedLines` — total changed line count (Summarize bucketing threshold).
- `baseBranch` — the base branch name (verify/blame).
- `reviewConfig` — the parsed REVIEW.md object (thresholds + `ignore`), consumed by the Filter stage.
- `exclusionPatterns` — the parsed exclusion-pattern list, consumed by the Filter stage.
- `reviewConfigPath` — the REVIEW.md path (or `null`), carried for provenance.

**`verify` handoff (sha-scoped paths for the executor's pinned command):**

```
verify: {
  scriptPath: "{plugin_root}/scripts/verify_findings.py",
  inputPathBase: "{output_dir}/code-gauntlet-phase4-input-{head_sha_short}",
  outputPathBase: "{output_dir}/code-gauntlet-phase4-output-{head_sha_short}"
}
```

The skill supplies only the path base and slice sizing (`limits.verifySliceSize`). The Verify stage does the rest **inside the workflow**: it slices the mid-workflow merged findings, dispatches the artifact-writer to persist each `${inputPathBase}.slice{i}.json` (the workflow has no disk, so the writer materializes them before the executor loop), then dispatches one `executor` per slice to run `verify_findings.py --input <slice> --output <slice-out> --nonce {nonce}.{i} --head-sha {headShaShort} --base-branch {baseBranch} [--diff-file {diffPath}]` and return the receipt envelope verbatim. The skill never pre-writes slice contents — it cannot, since the merged findings do not exist until Discover→Merge run.

**`checkpoints` (resume only):** omit on a fresh run. On resume-from-checkpoint (Phase 8), set it to the content of the persisted checkpoint artifact so the workflow skips completed phases.

---

## Triage Announcement

Announce triage results before proceeding: PR title, review mode, file counts by risk level, AI-generated files if any, active dimensions. For 1000+ line PRs, add: "This PR is [N] lines. Review effectiveness drops sharply above 400 lines. Consider splitting into smaller PRs."
